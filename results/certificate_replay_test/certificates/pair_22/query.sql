SELECT
  person.firstname AS FIRSTNAME,
  person.lastname AS LASTNAME,
  address.city AS CITY,
  address.state AS STATE
FROM PERSON
LEFT JOIN ADDRESS
  ON address.personid = person.personid